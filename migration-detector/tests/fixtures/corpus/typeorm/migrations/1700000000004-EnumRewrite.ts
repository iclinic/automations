import {MigrationInterface, QueryRunner} from "typeorm";

export class EnumRewrite1700000000004 implements MigrationInterface {

    public async up(queryRunner: QueryRunner): Promise<any> {
        await queryRunner.query(`ALTER TYPE "public"."schedule_status_enum" RENAME TO "schedule_status_enum_old"`, undefined);
        await queryRunner.query(`CREATE TYPE "schedule_status_enum" AS ENUM('scheduled', 'finished')`, undefined);
        await queryRunner.query(`ALTER TABLE "schedule" ALTER COLUMN "status" DROP DEFAULT`, undefined);
        await queryRunner.query(`ALTER TABLE "schedule" ALTER COLUMN "status" TYPE "schedule_status_enum" USING "status"::"text"::"schedule_status_enum"`, undefined);
        await queryRunner.query(`ALTER TABLE "schedule" ALTER COLUMN "status" SET DEFAULT 'scheduled'`, undefined);
        await queryRunner.query(`DROP TYPE "schedule_status_enum_old"`, undefined);
    }

    public async down(queryRunner: QueryRunner): Promise<any> {
        await queryRunner.query(`SELECT 1`);
    }

}
