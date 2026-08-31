import {MigrationInterface, QueryRunner} from "typeorm";

export class DropColumn1700000000003 implements MigrationInterface {

    public async up(queryRunner: QueryRunner): Promise<any> {
        await queryRunner.query(`ALTER TABLE "schedule" DROP COLUMN "notes"`);
    }

    public async down(queryRunner: QueryRunner): Promise<any> {
        await queryRunner.query(`SELECT 1`);
    }

}
