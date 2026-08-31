import {MigrationInterface, QueryRunner} from "typeorm";

export class AmbiguousUp1700000000009 implements MigrationInterface {

    public async up(queryRunner: QueryRunner): Promise<any> {
        await queryRunner.query(`ALTER TABLE "schedule" DROP COLUMN "room"`);
    }

    public async up(queryRunner: QueryRunner): Promise<any> {
        await queryRunner.query(`SELECT 1`);
    }

    public async down(queryRunner: QueryRunner): Promise<any> {
        await queryRunner.query(`SELECT 1`);
    }

}
