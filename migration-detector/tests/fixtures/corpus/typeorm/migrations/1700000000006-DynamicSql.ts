import {MigrationInterface, QueryRunner} from "typeorm";

export class DynamicSql1700000000006 implements MigrationInterface {

    public async up(queryRunner: QueryRunner): Promise<any> {
        const table = "schedule";
        await queryRunner.query(`ALTER TABLE "${table}" DROP COLUMN "seats"`);
    }

    public async down(queryRunner: QueryRunner): Promise<any> {
        await queryRunner.query(`SELECT 1`);
    }

}
